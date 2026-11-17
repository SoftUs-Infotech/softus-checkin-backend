"""Work-from-home screen capture: storing screenshots, the punch-in gate, review.

The browser captures (an employee shares their screen once and the page keeps
a frame at a random moment in every interval); this service stores what
arrives, and decides whether a punch-in may go ahead. An administrator reviews
the screenshots and deletes them once looked at — the point of deleting is to
free the space, so a deletion removes the file as well as its record.
"""

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from bson import ObjectId

from app.core import rate_limit
from app.core.config import settings
from app.core.datetime import attendance_date, date_key, to_utc, utc_now
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.db.mongo import Collections, get_collection
from app.models.audit_log import AuditAction, AuditEntity
from app.models.screenshot import Screenshot
from app.models.user import WorkMode, works_from_home
from app.schemas.screenshot import ScreenshotDelete
from app.services import audit
from app.services import settings as settings_service
from app.services.audit import Actor
from app.utils.storage import get_storage

logger = get_logger(__name__)

#: A punch-in from home needs a screenshot at most this old. The page takes a
#: fresh one the moment "punch in" is pressed, so this only has to cover the
#: round trip — it is not a grace period for capture being off.
CAPTURE_FRESHNESS_SECONDS = 180

#: While sharing, the page checks in every 30 seconds. Nothing heard for this
#: long means the screen is no longer shared — a closed tab, a sleeping laptop,
#: a crashed browser — and the session is ended at the last moment it was.
#: Generous enough for a background tab, which browsers may slow to one timer
#: a minute.
CAPTURE_TIMEOUT_SECONDS = 180

#: A random moment in every 5–60 minutes, plus one per punch-in, is a handful
#: an hour. This is far above that and only there to stop a runaway loop
#: filling the bucket.
UPLOAD_LIMIT = rate_limit.RateLimit(
    30, 600, "Too many screenshots in a short time. Try again in a few minutes."
)

#: Deletions are processed this many records at a time.
_DELETE_CHUNK = 1000


def _shots():
    return get_collection(Collections.SCREENSHOTS)


def _users():
    return get_collection(Collections.USERS)


def _heartbeats():
    return get_collection(Collections.CAPTURE_HEARTBEATS)


def require_wfh(user: dict[str, Any]) -> None:
    if not works_from_home(user):
        raise PermissionDeniedError(
            "Screen capture is only used for people working from home.",
            code="NOT_WORKING_FROM_HOME",
        )


def _sniff(data: bytes) -> tuple[str, str] | None:
    """The image type from its first bytes — the header the browser sent is a claim."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _object_id(value: str, what: str = "screenshot") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise NotFoundError(f"This {what} was not found.")
    return ObjectId(value)


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
async def save(
    user: dict[str, Any], data: bytes, *, width: int | None = None, height: int | None = None
) -> dict[str, Any]:
    """Store one screenshot from a work-from-home employee."""
    require_wfh(user)
    if not data:
        raise ValidationError("The screenshot arrived empty.", code="EMPTY_SCREENSHOT")
    if len(data) > settings.SCREENSHOT_MAX_BYTES:
        raise ValidationError(
            "The screenshot is too large to store.", code="SCREENSHOT_TOO_LARGE"
        )
    sniffed = _sniff(data)
    if sniffed is None:
        raise ValidationError("That file is not a JPEG, PNG or WebP image.", code="NOT_AN_IMAGE")
    content_type, extension = sniffed

    rate_limit.hit("screenshot_upload", user["id"], UPLOAD_LIMIT)

    now = utc_now()
    day = date_key(attendance_date(now))
    key = f"screenshots/{user['id']}/{day}/{now:%H%M%S}-{secrets.token_hex(4)}.{extension}"
    storage = get_storage()
    await storage.put(key, data, content_type)

    document = Screenshot(
        user_id=user["id"],
        date=day,
        captured_at=now,
        storage_key=key,
        storage_provider=storage.name,
        content_type=content_type,
        size_bytes=len(data),
        width=width,
        height=height,
    )
    try:
        result = await _shots().insert_one(document.to_mongo())
    except Exception:
        # A file nobody can find again is exactly the wasted space this feature
        # is meant to avoid.
        await storage.delete_many([key])
        raise
    # A screenshot is the strongest sign the screen is shared.
    await _mark_seen(user["id"], now)
    return {"id": str(result.inserted_id), "captured_at": now}


async def _mark_seen(user_id: str, at: datetime) -> None:
    await _heartbeats().update_one(
        {"_id": user_id}, {"$set": {"last_seen_at": at}}, upsert=True
    )


async def heartbeat(user: dict[str, Any]) -> datetime:
    """The page saying "the screen is still shared", every 30 seconds."""
    require_wfh(user)
    now = utc_now()
    await _mark_seen(user["id"], now)
    return now


async def last_seen_map(user_ids: list[str]) -> dict[str, datetime]:
    """When each person's shared screen was last heard from."""
    cursor = _heartbeats().find({"_id": {"$in": user_ids}})
    return {row["_id"]: to_utc(row["last_seen_at"]) async for row in cursor}


async def last_capture_at(user_id: str) -> datetime | None:
    latest = await _shots().find_one(
        {"user_id": user_id}, sort=[("captured_at", -1)], projection={"captured_at": 1}
    )
    return to_utc(latest["captured_at"]) if latest else None


async def require_recent_capture(user: dict[str, Any]) -> None:
    """The punch-in gate for people working from home.

    Checked against what the server actually received, not a flag the browser
    sends — a flag would be one edited request away from meaning nothing.
    """
    if not works_from_home(user):
        return
    last = await last_capture_at(user["id"])
    if last is None or utc_now() - last > timedelta(seconds=CAPTURE_FRESHNESS_SECONDS):
        raise PermissionDeniedError(
            "You are working from home, so screen capture has to be running before "
            "you punch in. Start screen capture and try again.",
            code="SCREEN_CAPTURE_REQUIRED",
        )


async def interval_minutes() -> int:
    org = await settings_service.get_settings()
    return int(org.get("wfh_screenshot_interval_minutes") or 10)


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
def _to_out(document: dict[str, Any]) -> dict[str, Any]:
    storage = get_storage(document.get("storage_provider"))
    return {
        "id": str(document["_id"]),
        "user_id": document["user_id"],
        "date": document["date"],
        "captured_at": to_utc(document["captured_at"]),
        "content_type": document["content_type"],
        "size_bytes": document["size_bytes"],
        "width": document.get("width"),
        "height": document.get("height"),
        "url": storage.url_for(document["storage_key"], document["content_type"]),
    }


def _person(user: dict[str, Any], stats: dict[str, Any] | None) -> dict[str, Any]:
    first, last = user.get("first_name") or "", user.get("last_name") or ""
    stats = stats or {}
    return {
        "id": str(user["_id"]),
        "full_name": f"{first} {last}".strip() or user.get("email", ""),
        "email": user.get("email", ""),
        "employee_code": user.get("employee_code"),
        "designation": user.get("designation"),
        "department": user.get("department"),
        "work_mode": user.get("work_mode") or WorkMode.OFFICE.value,
        "status": user.get("status"),
        "removed": bool(user.get("is_deleted")),
        "on_day": stats.get("on_day", 0),
        "last_captured_at": to_utc(stats["last"]) if stats.get("last") else None,
        "total_count": stats.get("count", 0),
        "total_bytes": stats.get("bytes", 0),
    }


async def summary(day: date) -> dict[str, Any]:
    """Everybody working from home, plus anybody whose screenshots are still stored.

    The second group matters for space: somebody moved back to the office, or
    removed, still has files in the bucket until an administrator deletes them.
    """
    key = date_key(day)
    stats: dict[str, dict[str, Any]] = {}
    pipeline = [
        {
            "$group": {
                "_id": "$user_id",
                "count": {"$sum": 1},
                "bytes": {"$sum": "$size_bytes"},
                "last": {"$max": "$captured_at"},
                "on_day": {"$sum": {"$cond": [{"$eq": ["$date", key]}, 1, 0]}},
            }
        }
    ]
    async for row in _shots().aggregate(pipeline):
        stats[row["_id"]] = row

    people: dict[str, dict[str, Any]] = {}
    async for user in _users().find({"is_deleted": False, "work_mode": WorkMode.WFH.value}):
        people[str(user["_id"])] = user

    stray = [ObjectId(uid) for uid in stats if uid not in people and ObjectId.is_valid(uid)]
    if stray:
        # Removed accounts included on purpose: their files are still there.
        async for user in _users().find({"_id": {"$in": stray}}):
            people[str(user["_id"])] = user

    rows = [_person(user, stats.get(uid)) for uid, user in people.items()]
    rows.sort(key=lambda row: (row["work_mode"] != WorkMode.WFH.value, row["full_name"].lower()))

    return {
        "date": key,
        "interval_minutes": await interval_minutes(),
        "totals": {
            "people_wfh": sum(1 for row in rows if row["work_mode"] == WorkMode.WFH.value),
            "on_day": sum(row["on_day"] for row in rows),
            "count": sum(row["total_count"] for row in rows),
            "size_bytes": sum(row["total_bytes"] for row in rows),
        },
        "people": rows,
    }


async def list_for(user_id: str, day: date) -> list[dict[str, Any]]:
    """One person's screenshots for a day, in the order they were taken."""
    _object_id(user_id, "employee")
    cursor = _shots().find({"user_id": user_id, "date": date_key(day)}).sort("captured_at", 1)
    return [_to_out(document) async for document in cursor]


async def days_for(user_id: str, limit: int = 120) -> list[dict[str, Any]]:
    """The days this person has screenshots on, newest first."""
    _object_id(user_id, "employee")
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$date", "count": {"$sum": 1}, "size_bytes": {"$sum": "$size_bytes"}}},
        {"$sort": {"_id": -1}},
        {"$limit": limit},
    ]
    return [
        {"date": row["_id"], "count": row["count"], "size_bytes": row["size_bytes"]}
        async for row in _shots().aggregate(pipeline)
    ]


# --------------------------------------------------------------------------- #
# deletion
# --------------------------------------------------------------------------- #
def _delete_query(scope: ScreenshotDelete) -> dict[str, Any]:
    if scope.ids is not None:
        return {"_id": {"$in": [_object_id(value) for value in scope.ids]}}
    if scope.everything:
        return {}
    query: dict[str, Any] = {}
    if scope.user_id is not None:
        _object_id(scope.user_id, "employee")
        query["user_id"] = scope.user_id
    if scope.date is not None:
        query["date"] = date_key(scope.date)
    return query


async def _label(scope: ScreenshotDelete, count: int) -> str:
    noun = f"{count} screenshot{'s' if count != 1 else ''}"
    if scope.everything:
        return f"All screenshots ({noun})"
    who = ""
    if scope.user_id:
        user = await _users().find_one({"_id": ObjectId(scope.user_id)}, {"email": 1})
        who = f" of {user['email']}" if user else ""
    when = f" on {date_key(scope.date)}" if scope.date else ""
    return f"{noun}{who}{when}"


async def delete(scope: ScreenshotDelete, actor: Actor) -> dict[str, int]:
    """Delete files and their records. A record goes only once its file has."""
    documents = await _shots().find(
        _delete_query(scope),
        {"storage_key": 1, "storage_provider": 1, "size_bytes": 1},
    ).to_list(length=None)
    if not documents:
        return {"deleted": 0, "freed_bytes": 0, "failed": 0}

    deleted = freed = 0
    for start in range(0, len(documents), _DELETE_CHUNK):
        chunk = documents[start : start + _DELETE_CHUNK]
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for document in chunk:
            by_provider.setdefault(document.get("storage_provider") or "local", []).append(document)

        for provider, items in by_provider.items():
            gone = await get_storage(provider).delete_many([item["storage_key"] for item in items])
            removed = [item for item in items if item["storage_key"] in gone]
            if not removed:
                continue
            await _shots().delete_many({"_id": {"$in": [item["_id"] for item in removed]}})
            deleted += len(removed)
            freed += sum(item.get("size_bytes", 0) for item in removed)

    failed = len(documents) - deleted
    if failed:
        logger.warning("%d screenshot file(s) could not be deleted and were kept.", failed)

    await audit.record(
        actor,
        AuditAction.DELETE,
        AuditEntity.SCREENSHOT,
        entity_id=scope.user_id,
        entity_label=await _label(scope, deleted),
        after={
            "deleted": deleted,
            "freed_bytes": freed,
            "failed": failed,
            "date": date_key(scope.date) if scope.date else None,
            "everything": scope.everything,
        },
    )
    return {"deleted": deleted, "freed_bytes": freed, "failed": failed}
