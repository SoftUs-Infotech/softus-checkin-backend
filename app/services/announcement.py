"""Announcements and events.

Publishing notifies the audience. Editing does not re-notify — nobody wants a
second alert because a typo was fixed.
"""

from typing import Any

from bson import ObjectId

from app.core.datetime import date_key, org_today, utc_now
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.announcement import Announcement, AnnouncementType, NotificationType
from app.models.audit_log import AuditAction, AuditEntity
from app.models.base import serialize
from app.schemas.announcement import AnnouncementCreate, AnnouncementUpdate
from app.services import audit
from app.services import notification as notification_service
from app.services.audit import Actor

logger = get_logger(__name__)


def _announcements():
    return get_collection(Collections.ANNOUNCEMENTS)


def _users():
    return get_collection(Collections.USERS)


async def require_announcement(announcement_id: str) -> dict[str, Any]:
    if not ObjectId.is_valid(announcement_id):
        raise NotFoundError("Announcement not found.", code="ANNOUNCEMENT_NOT_FOUND")
    document = await _announcements().find_one(
        {"_id": ObjectId(announcement_id), "is_deleted": False}
    )
    if document is None:
        raise NotFoundError("Announcement not found.", code="ANNOUNCEMENT_NOT_FOUND")
    return serialize(document)


async def _audience_ids(departments: list[str]) -> list[str]:
    query: dict[str, Any] = {"status": "ACTIVE", "is_deleted": False}
    if departments:
        query["department"] = {"$in": departments}
    users = await _users().find(query, {"_id": 1}).to_list(length=1000)
    return [str(user["_id"]) for user in users]


async def list_announcements(
    *, department: str | None = None, include_unpublished: bool = False, limit: int = 20
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"is_deleted": False}
    if not include_unpublished:
        query["is_published"] = True
    if department is not None:
        query["$or"] = [
            {"audience_departments": []},
            {"audience_departments": department},
        ]

    cursor = (
        _announcements()
        .find(query)
        .sort([("pinned", -1), ("created_at", -1)])
        .limit(limit)
    )
    return [serialize(document) for document in await cursor.to_list(length=limit)]


async def upcoming_events(limit: int = 5) -> list[dict[str, Any]]:
    """Events from today onwards — the dashboard's 'upcoming' panel."""
    cursor = (
        _announcements()
        .find(
            {
                "is_deleted": False,
                "is_published": True,
                "type": AnnouncementType.EVENT.value,
                "event_date": {"$gte": date_key(org_today())},
            }
        )
        .sort("event_date", 1)
        .limit(limit)
    )
    return [serialize(document) for document in await cursor.to_list(length=limit)]


async def create_announcement(
    payload: AnnouncementCreate, author: dict[str, Any], actor: Actor
) -> dict[str, Any]:
    now = utc_now()
    announcement = Announcement(
        title=payload.title,
        body=payload.body,
        type=payload.type,
        event_date=date_key(payload.event_date) if payload.event_date else None,
        event_time=payload.event_time,
        location=payload.location,
        audience_departments=payload.audience_departments,
        pinned=payload.pinned,
        is_published=payload.is_published,
        published_at=now if payload.is_published else None,
        author_id=author.get("id"),
        author_name=(
            f"{author.get('first_name', '')} {author.get('last_name', '')}".strip()
            or author.get("email")
        ),
    )
    result = await _announcements().insert_one(announcement.to_mongo())
    created = await require_announcement(str(result.inserted_id))

    if payload.is_published:
        await notification_service.notify_many(
            await _audience_ids(payload.audience_departments),
            type=NotificationType.ANNOUNCEMENT,
            title=payload.title,
            body=payload.body[:200],
            link=f"/announcements/{created['id']}",
        )

    await audit.record(
        actor,
        AuditAction.CREATE,
        AuditEntity.SETTINGS,
        entity_id=created["id"],
        entity_label=f"Announcement: {payload.title}",
        after={"title": payload.title, "type": payload.type.value},
    )
    return created


async def update_announcement(
    announcement_id: str, payload: AnnouncementUpdate, actor: Actor
) -> dict[str, Any]:
    existing = await require_announcement(announcement_id)
    changes = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not changes:
        return existing

    if "event_date" in changes:
        changes["event_date"] = date_key(changes["event_date"])
    if changes.get("is_published") and not existing.get("published_at"):
        changes["published_at"] = utc_now()

    changes["updated_at"] = utc_now()
    await _announcements().update_one(
        {"_id": ObjectId(announcement_id)}, {"$set": changes}
    )

    updated = await require_announcement(announcement_id)
    await audit.record_change(
        actor,
        AuditAction.UPDATE,
        AuditEntity.SETTINGS,
        entity_id=announcement_id,
        entity_label=f"Announcement: {updated['title']}",
        before=existing,
        after=updated,
    )
    return updated


async def delete_announcement(announcement_id: str, actor: Actor) -> None:
    existing = await require_announcement(announcement_id)
    await _announcements().update_one(
        {"_id": ObjectId(announcement_id)},
        {"$set": {"is_deleted": True, "is_published": False, "updated_at": utc_now()}},
    )
    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.SETTINGS,
        entity_id=announcement_id,
        entity_label=f"Announcement: {existing['title']}",
        before={"title": existing["title"]},
    )
