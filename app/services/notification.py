"""Telling somebody something happened.

In-app first: polled rather than pushed — the bell icon refetches. Creating a
notification never raises, because an employee not being told about an approval
must not roll back the approval itself.

An email can ride along on the same call. That is deliberate rather than two
calls at each site: the bell and the inbox should never be able to disagree
about whether a decision was made, and keeping them in one place is what stops
somebody adding a new event and remembering only half of it. Delivery is
best-effort in exactly the same way — ``send_email`` swallows its own failures,
so a wrong SMTP password costs an email, never an approval.
"""

from datetime import timedelta
from typing import Any

from bson import ObjectId

from app.core.datetime import utc_now
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import Notification, NotificationType
from app.models.base import serialize
from app.schemas.common import PaginationParams
from app.utils.email import EmailMessage, send_email_later

logger = get_logger(__name__)


def _notifications():
    return get_collection(Collections.NOTIFICATIONS)


async def notify(
    user_id: str,
    *,
    type: NotificationType,
    title: str,
    body: str | None = None,
    link: str | None = None,
    email: EmailMessage | None = None,
) -> None:
    """Put this in the bell, and — when an ``email`` is given — the inbox too."""
    try:
        await _notifications().insert_one(
            Notification(
                user_id=user_id, type=type, title=title, body=body, link=link
            ).to_mongo()
        )
    except Exception:  # noqa: BLE001 - never break the operation that triggered it
        logger.exception("Could not create a notification for %s", user_id)

    if email is not None:
        # Best-effort and off the request: the decision is already recorded,
        # and nobody should wait on a mail server to see that it was.
        send_email_later(email)


async def notify_many(
    user_ids: list[str],
    *,
    type: NotificationType,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> None:
    if not user_ids:
        return
    try:
        await _notifications().insert_many(
            [
                Notification(
                    user_id=user_id, type=type, title=title, body=body, link=link
                ).to_mongo()
                for user_id in user_ids
            ]
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not create notifications for %d user(s)", len(user_ids))


async def notify_admins(
    *,
    type: NotificationType,
    title: str,
    body: str | None = None,
    link: str | None = None,
    exclude_user_id: str | None = None,
) -> None:
    """Tell every administrator that something needs a decision."""
    query: dict[str, Any] = {"role": "ADMIN", "status": "ACTIVE", "is_deleted": False}
    admins = await get_collection(Collections.USERS).find(query).to_list(length=100)
    user_ids = [
        str(admin["_id"]) for admin in admins if str(admin["_id"]) != exclude_user_id
    ]
    await notify_many(user_ids, type=type, title=title, body=body, link=link)


async def notify_approvers(
    subject_user_id: str,
    *,
    type: NotificationType,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> None:
    """Tell whoever decides this employee's requests.

    That is every administrator, plus the employee's own reporting manager —
    the person most likely to know whether the reason is true, and the one the
    request will actually be waiting on.
    """
    await notify_admins(
        type=type, title=title, body=body, link=link, exclude_user_id=subject_user_id
    )

    subject = await get_collection(Collections.USERS).find_one(
        {"_id": ObjectId(subject_user_id)}
    )
    manager_id = (subject or {}).get("reporting_manager_id")
    if not manager_id or str(manager_id) == subject_user_id:
        return

    manager = await get_collection(Collections.USERS).find_one(
        {"_id": ObjectId(str(manager_id)), "status": "ACTIVE", "is_deleted": False}
    )
    if manager and manager.get("role") == "MANAGER":
        await notify(str(manager["_id"]), type=type, title=title, body=body, link=link)


async def list_notifications(
    user_id: str, pagination: PaginationParams, *, unread_only: bool = False
) -> tuple[list[dict[str, Any]], int]:
    query: dict[str, Any] = {"user_id": user_id, "is_deleted": False}
    if unread_only:
        query["read_at"] = None

    total = await _notifications().count_documents(query)
    cursor = (
        _notifications()
        .find(query)
        .sort("created_at", -1)
        .skip(pagination.skip)
        .limit(pagination.limit)
    )
    items = [serialize(document) for document in await cursor.to_list(length=pagination.limit)]
    return items, total


async def unread_count(user_id: str) -> int:
    return await _notifications().count_documents(
        {"user_id": user_id, "read_at": None, "is_deleted": False}
    )


async def mark_read(user_id: str, notification_id: str) -> bool:
    if not ObjectId.is_valid(notification_id):
        return False
    result = await _notifications().update_one(
        {"_id": ObjectId(notification_id), "user_id": user_id, "read_at": None},
        {"$set": {"read_at": utc_now(), "updated_at": utc_now()}},
    )
    return result.modified_count > 0


async def mark_all_read(user_id: str) -> int:
    result = await _notifications().update_many(
        {"user_id": user_id, "read_at": None},
        {"$set": {"read_at": utc_now(), "updated_at": utc_now()}},
    )
    return result.modified_count


async def weekly_digest() -> int:
    """Monday's summary for administrators. Returns how many were told.

    A digest exists so the system works for someone who never opens it: the
    week's exceptions arrive whether or not anybody went looking. It reports
    only what needs a decision or a chase — a list of everyone who turned up as
    expected is not news.
    """
    from app.core.datetime import date_key, org_today
    from app.services import correction as correction_service
    from app.services import leave as leave_service
    from app.services import task as task_service

    corrections = await correction_service.pending_count()
    leave = await leave_service.pending_count()
    tasks = await task_service.pending_count()

    last_week_start = org_today() - timedelta(days=7)
    exceptions = await get_collection(Collections.ATTENDANCE).count_documents(
        {
            "date": {"$gte": date_key(last_week_start), "$lt": date_key(org_today())},
            "status": {"$in": ["MISSING_PUNCH_OUT", "ABSENT"]},
        }
    )

    if not any((corrections, leave, tasks, exceptions)):
        return 0

    await notify_admins(
        type=NotificationType.APPROVAL_PENDING,
        title="Last week at a glance",
        body=(
            f"{corrections} correction(s), {leave} leave request(s) and {tasks} task(s) "
            f"are waiting for a decision. {exceptions} day(s) last week were absent or "
            "left without a punch-out."
        ),
        link="/admin",
    )
    admins = await get_collection(Collections.USERS).count_documents(
        {"role": "ADMIN", "status": "ACTIVE", "is_deleted": False}
    )
    return admins
